# type: ignore
"""admin: split out of main.py. Cross-module calls are module-qualified so a test patching the owner module affects every caller (see plans/PLAN-split-main-py.md)."""
import logging
import time
import random
import getpass
import tqdm
from datetime import datetime
from datetime import timedelta
from hotel import db
from hotel import ui
from hotel import reports
from hotel import clearance_ui
from hotel import session as _mod_session
from hotel import billing
from hotel import concierge
from hotel import core
from hotel import customer
from hotel import items
from hotel import keycards
from hotel import loyalty
from hotel import notifications
from hotel import onboarding
from hotel import orders
from hotel import reservations as _mod_reservations
from hotel import rooms

__all__ = [
    'clear_lockout',
    'edit_user',
    'admin_login',
    'add_user',
    'delete_user',
    'view_users',
    '_run_report',
    'export_reports_menu',
    '_admin_menu',
    '_run_admin_submenu',
    'admin_panel',
    'view_discount_codes',
    'manage_discount_codes',
    'reset_user_password',
    'IT_SYSTEMS',
    'IT_SOFTWARE',
    'IT_TROUBLESHOOTING',
    '_pick_from_list',
    'it_network_configuration',
    'it_user_accounts',
    'it_system_diagnostics',
    'it_software_installation',
    'it_support_panel',
    'valet_vehicle_management',
    '_valet_list_parked',
]




def clear_lockout(target_username: str) -> bool:
    """Clear failed attempts and lockout for a user. Returns True on success."""
    if not target_username:
        return False
    with db.get_connection() as conn:
        if conn is None:
            return False
        try:
            cursor = conn.cursor()
            cursor.execute("UPDATE Users SET FailedAttempts = 0, LockoutTime = NULL WHERE Username = ?", (target_username,))
            conn.commit()
            logging.info("Cleared lockout for user: %s", target_username)
            return True
        except Exception as e:
            logging.error("Failed to clear lockout: %s", e)
            return False

def edit_user():
    """Edit an existing user."""
    try:
        username = input("Enter the username of the user to edit: ").strip()
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM Users WHERE Username = ?", (username,))
            user = cursor.fetchone()

            if user:
                logging.info(f"Current user details: Username: {user.Username}")

                new_username = input("Enter new username (leave blank to keep current): ").strip()
                new_password = input("Enter new password (leave blank to keep current): ").strip()

                if new_username == "":
                    new_username = user.Username
                if new_password == "":
                    new_password = user.Password

                cursor.execute("""
                    UPDATE Users
                    SET Username = ?, Password = ?
                    WHERE Username = ?""",
                    (new_username, new_password, username))
                conn.commit()
                changes = []
                if new_username != username:
                    changes.append(f"username {username} -> {new_username}")
                if new_password != user.Password:
                    changes.append("password reset")
                core.log_audit("UPDATE", "User", new_username,
                          "; ".join(changes) if changes else "no effective change")
                logging.info(f"User '{username}' updated successfully.")
            else:
                logging.info("No user found with that username.")
    except Exception as e:
        logging.error(f"Error editing user: {e}")


def admin_login():
    """Handle admin login with a grace period for lockout, displaying a warning before the final lockout."""
    while True:
        try:
            username = input("Enter admin username: ").strip()
            password = getpass.getpass("Enter admin password: ").strip()

            with db.get_connection() as conn:
                if conn is None:
                    logging.error("Database connection failed during login.")
                    return False, None, False
                cursor = conn.cursor()
                cursor.execute("SELECT Password, FailedAttempts, LockoutTime, Role FROM Users WHERE Username = ?", (username,))
                user = cursor.fetchone()

                if not user:
                    logging.info("Username not found.")
                    continue

                db_password, failed_attempts, lockout_time, role = user

                # Check if the user is currently locked out
                if lockout_time and lockout_time > datetime.now():
                    logging.info(f"Account is locked until {lockout_time}. Please try again later.")
                    continue

                # Validate the entered password
                if password == db_password:
                    logging.info("Login successful!")
                    # Reset failed attempts and lockout time
                    cursor.execute("UPDATE Users SET FailedAttempts = 0, LockoutTime = NULL WHERE Username = ?", (username,))
                    conn.commit()
                    _mod_session.CURRENT_USER = username
                    core.log_audit("LOGIN", "User", username, f"Role {role}")
                    return True, role, False  # Return role and reauthentication status
                else:
                    failed_attempts = (failed_attempts or 0) + 1
                    logging.info(f"Invalid credentials. Attempt {failed_attempts}/{core.get_lockout_threshold()}.")
                    core.log_audit("LOGIN_FAILED", "User", username,
                              f"Failed attempt {failed_attempts}/{core.get_lockout_threshold()}")

                    # Display a warning message after the second failed attempt
                    if failed_attempts == core.get_lockout_threshold() - 1:
                        logging.info("Warning: One more failed attempt will lock you out.")

                    # Lock out the user after exceeding the threshold
                    if failed_attempts >= core.get_lockout_threshold():
                        lockout_time = datetime.now() + timedelta(minutes=core.get_lockout_duration())
                        cursor.execute("UPDATE Users SET FailedAttempts = ?, LockoutTime = ? WHERE Username = ?", (failed_attempts, lockout_time, username))
                        conn.commit()
                        logging.info("Maximum login attempts exceeded. Account locked.")
                        core.log_audit("LOCKOUT", "User", username,
                                  f"Locked until {lockout_time} after {failed_attempts} failed attempts")
                        unlockpassword = input("Would you like to attempt manager override to unlock this account? (Y/N) ")
                        if unlockpassword.upper() == "Y":
                            if core.require_master_override():
                                cursor.execute("UPDATE Users SET FailedAttempts = 0, LockoutTime = NULL WHERE Username = ?", (username,))
                                conn.commit()
                                logging.info("Account unlocked successfully!")
                                return False, role, True  # Return role and reauthentication status
                            else:
                                logging.info("Invalid master override secret.")
                        else:
                            logging.info("Manager override not attempted.")
                            return False, None, False
                    else:
                        cursor.execute("UPDATE Users SET FailedAttempts = ? WHERE Username = ?", (failed_attempts, username))
                        conn.commit()

        except Exception as e:
            logging.error(f"Error during login: {e}")
            return False, None, False


def add_user():
    try:
        new_username = input("Enter new username: ").strip()
        new_password = input("Enter new password: ").strip()
        role = core._prompt_role()
        if not role:
            logging.info("No account was created.")
            return
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM Users WHERE Username = ?", (new_username,))
            if cursor.fetchone():
                logging.info(f"User '{new_username}' already exists.")
                return
            cursor.execute("INSERT INTO Users (Username, Password, Role) VALUES (?, ?, ?)", (new_username, new_password, role))
            conn.commit()
            core.log_audit("CREATE", "User", new_username, f"Role {role}")
            logging.info(f"User '{new_username}' added successfully.")
    except Exception as e:
        logging.error(f"Error adding user: {e}")
    # connection closed by context manager

def delete_user():
    try:
        del_username = input("Enter username to delete: ").strip()
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            cursor.execute("SELECT Role FROM Users WHERE Username = ?", (del_username,))
            existing = cursor.fetchone()
            if not existing:
                logging.info(f"User '{del_username}' not found.")
                return
            cursor.execute("DELETE FROM Users WHERE Username = ?", (del_username,))
            conn.commit()
            core.log_audit("DELETE", "User", del_username, f"Deleted account with role {existing[0]}")
            logging.info(f"User '{del_username}' deleted successfully.")
    except Exception as e:
        logging.error(f"Error deleting user: {e}")
    # connection closed by context manager

def view_users():
    """Display users; optionally show passwords after verifying the master secret."""
    try:
        with db.get_connection() as conn:
            if conn is None:
                return
            cursor = conn.cursor()
            passwords = input("Would you like to see the passwords? (Y/N): ").strip().upper()
            show_passwords = False
            if passwords == 'Y':
                # Same check every other master-gated screen uses.
                if core.require_master_override(prompt="Please enter the master password: "):
                    logging.info("Master password is correct. Displaying passwords.")
                    show_passwords = True
                else:
                    logging.info("Incorrect master password. Cannot display passwords.")

            cursor.execute("SELECT Username, Password, Role FROM Users")
            rows = cursor.fetchall()
            headers = ["Username", "Password", "Role"] if show_passwords else ["Username", "Role"]
            table_rows = []
            for user in rows:
                if user.Username.lower() == 'master':
                    continue
                if show_passwords:
                    table_rows.append((user.Username, user.Password, user.Role))
                else:
                    table_rows.append((user.Username, user.Role))
            ui.show_table("Users and Roles", headers, table_rows)
    except Exception as e:
        logging.error(f"Error displaying users: {e}")


def _run_report(label, fn, **kwargs):
    """Run a reports.py export and show the result. Returns True on success."""
    try:
        for path in fn(**kwargs):
            ui.success(f"{label} exported: {path}")
        return True
    except Exception as e:
        logging.error(f"Error exporting {label.lower()}: {e}")
        return False


def export_reports_menu():
    """Interactive admin submenu for report exports."""
    while True:
        ui.pause()
        ui.show_menu("Report Export Menu", [
            "1. Export Transactions",
            "2. Export Reservations",
            "3. Export Loyalty Statements",
            "4. Export Invoices (split folio)",
            "5. Export Revenue (room vs F&B)",
            "6. Export Occupancy",
            "7. Export Housekeeping Board",
            "8. Export Guest Satisfaction",
            "9. Export Audit Log",
            "10. Export Booking Ledger",
            "11. Back to Admin Panel",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            _run_report("Transactions", reports.export_transactions)
        elif choice == '2':
            _run_report("Reservations", reports.export_reservations)
        elif choice == '3':
            # A loyalty statement is the history of a GUEST. Both references resolve to
            # one person; a room is accepted because staff mostly know the room, and it
            # then reports that guest's whole history rather than one room's fragment.
            # A room number and a CustomerID are both 4 digits, so the type is asked
            # for rather than guessed from the shape of the input.
            who = input("Guest ID/email/name, or a room number (leave blank for all): ").strip()
            if not who:
                _run_report("Loyalty statements", reports.export_loyalty_statements)
            else:
                ui.show_menu("Loyalty statement for", ["1. Guest (ID, email, or name)", "2. Room number"])
                ref_type = input("Enter your choice: ").strip()
                if ref_type == '1':
                    _run_report("Loyalty statements", reports.export_loyalty_statements,
                                customer=who)
                else:
                    _run_report("Loyalty statements", reports.export_loyalty_statements,
                                room_number=who)
        elif choice == '4':
            _run_report("Invoices", reports.export_invoices)
        elif choice == '5':
            start = ui.ask_optional_date("From date")
            end = ui.ask_optional_date("To date")
            _run_report("Revenue", reports.export_revenue, start_date=start, end_date=end)
        elif choice == '6':
            start = ui.ask_optional_date("From date (blank = first night on record)")
            end = ui.ask_optional_date("To date (blank = the business date)")
            _run_report("Occupancy", reports.export_occupancy, start_date=start, end_date=end)
        elif choice == '7':
            floor = input("Floor (blank for every floor): ").strip()
            # Defaults to the business date, and takes an explicit one, so the board for
            # a day that has already closed can be re-run rather than only today's.
            on_date = ui.ask_optional_date(f"Board date (blank = business date, "
                                           f"{core.business_date()})")
            _run_report("Housekeeping", reports.export_housekeeping, floor=floor or None,
                        on_date=on_date)
        elif choice == '8':
            _run_report("Guest satisfaction", reports.export_guest_satisfaction)
        elif choice == '9':
            _run_report("Audit log", reports.export_audit_log)
        elif choice == '10':
            ref = input("Booking reference (blank for all): ").strip()
            _run_report("Booking ledger", reports.export_booking_ledger,
                        booking_ref=ref or None)
        elif choice == '11':
            break
        else:
            logging.info("Invalid choice. Please try again.")


def _admin_menu(role):
    """The Admin Panel's category menu for `role`, as DATA.

    Returns [(category_label, [(item_label, func, kwargs), ...] | None), ...],
    where a `None` entry list marks the "Exit Admin Panel" line. One list drives
    BOTH the rendering and the dispatch (see admin_panel), so an entry and the
    function it runs cannot drift apart -- the flat per-role menus this replaced
    could: the admin list had no 26, and each role re-typed the same reservation
    block by hand.

    It is rebuilt on every call because it closes over functions defined further
    down this file. Parity with the old flat menus is pinned by
    tests/test_admin_menu.py: a role must reach exactly the functions it reached
    before, no more and no less -- restructuring a menu is not a permission
    change.
    """
    def entry(label, func, **kwargs):
        return (label, func, kwargs)

    reservations = [
        entry("Add Reservation", _mod_reservations.add_reservation),
        entry("Edit Reservation", _mod_reservations.edit_reservation),
        entry("Delete Reservation", _mod_reservations.delete_reservation),
        entry("View Reservations", _mod_reservations.view_reservations),
        entry("Search Reservations", _mod_reservations.search_reservations),
        entry("Search Availability", _mod_reservations.show_availability_search),
        entry("Arrivals / Departures Board", _mod_reservations.show_arrivals_departures_board),
    ]
    guest_services = [
        entry("Guest Requests (Concierge & Feedback)", concierge.guest_requests_menu),
        entry("Order Management", orders.manage_orders_menu),
        entry("Staff Alerts", notifications.view_staff_alerts),
    ]
    items_and_services = [
        entry("Add Item", items.add_item),
        entry("Delete Item", items.delete_item),
        entry("Update Item", items.update_item),
        entry("View Items", items.view_items),
    ]
    clearance_desk = entry("Clearance Card Desk", clearance_ui.open_clearance_window)

    if role == 'staff':
        return [
            ("Reservations Management", reservations),
            ("Guest Services", guest_services),
            ("Rooms & Housekeeping", [entry("Rooms & Housekeeping", rooms.rooms_admin_menu, view_only=True)]),
            ("Perks", [entry("Post Complimentary Charge (tier perk)", items.comp_item_to_room), clearance_desk]),
            ("Exit Admin Panel", None),
        ]
    if role == 'manager':
        return [
            ("Reservations Management", reservations),
            ("Guest Services", guest_services + [entry("Send Notification to Customer", notifications.send_notification_to_customer)]),
            ("Items & Services", items_and_services),
            ("Accounts", [
                entry("View Users", view_users),
                entry("Search & View Guest Accounts", customer.search_customer_profiles),
            ]),
            ("Discounts & Pricing", [entry("View Discount Codes", view_discount_codes)]),
            ("Rooms & Housekeeping", [entry("Rooms & Housekeeping", rooms.rooms_admin_menu, view_only=True)]),
            ("Invoices & Reports", [entry("Invoices & Printing", billing.invoices_menu)]),
            ("Security & Access", [
                entry("Door Access Control", keycards.door_access_menu, view_only=True),
                clearance_desk,
            ]),
            ("Exit Admin Panel", None),
        ]
    if role == 'admin':
        return [
            ("Reservations Management", reservations),
            ("Guest Services", guest_services + [
                entry("Send Notification to Customer", notifications.send_notification_to_customer),
                entry("Send Alert to Staff", notifications.send_alert_to_staff),
            ]),
            ("Items & Services", items_and_services + [
                entry("Manage Amenities", orders.manage_amenities_menu),
                entry("Manage Promotions", orders.manage_promotions_menu),
            ]),
            ("Rooms & Housekeeping", [entry("Rooms & Housekeeping", rooms.rooms_admin_menu)]),
            ("Accounts", [
                entry("Add User", add_user),
                entry("Delete User", delete_user),
                entry("Edit User", edit_user),
                entry("View Users", view_users),
                entry("Reset User Password", reset_user_password),
                entry("Search & View Guest Accounts", customer.search_customer_profiles),
                entry("Create Guest Account", customer.create_guest_account),
                entry("Edit Guest Account", customer.edit_guest_account),
                entry("Reset Guest Password", customer.reset_guest_password),
                entry("Link Stay to Guest Account", customer.link_stay_to_guest_account),
                entry("Delete Guest Account", customer.delete_guest_account),
            ]),
            ("Loyalty", [entry("Loyalty Management", loyalty.loyalty_admin_menu)]),
            ("Discounts & Pricing", [
                entry("Manage Discount Codes", manage_discount_codes),
                entry("Manage Pricing & Settings", items.manage_pricing_rules),
            ]),
            ("Invoices & Reports", [
                entry("Invoices & Printing", billing.invoices_menu),
                entry("Export Reports", export_reports_menu),
            ]),
            ("Security & Access", [
                entry("Door Access Control", keycards.door_access_menu),
                clearance_desk,
            ]),
            ("Setup & Destructive", [
                entry("Setup Checklist", onboarding.onboarding_checklist, role=role),
                entry("Delete All Reservations (master override)", _mod_reservations.delete_all_reservations),
            ]),
            ("Exit Admin Panel", None),
        ]
    return []


def _run_admin_submenu(category, entries, session):
    """Show one category's submenu and run the chosen entry, then return.

    The label on screen and the function that runs come from the same tuple, so
    there is no numbering to keep in sync. A pause follows every action or
    rejected input so its message survives until the submenu redraws
    (docs/DEVIATIONS.md §12); entering the submenu does not pause, because the
    category choice that brought you here has already been read.
    """
    while True:
        ui.show_menu(
            category,
            [f"{i}. {label}" for i, (label, _func, _kwargs) in enumerate(entries, 1)]
            + [f"{len(entries) + 1}. Back to Admin Panel"],
            subtitle=session,
        )
        raw = input("Enter your choice: ").strip()
        if not raw.isdigit() or not 1 <= int(raw) <= len(entries) + 1:
            logging.info("Invalid choice. Please try again.")
            ui.pause()
            continue
        pick = int(raw)
        if pick == len(entries) + 1:
            return
        _label, func, kwargs = entries[pick - 1]
        func(**kwargs)
        ui.pause()


def admin_panel():

    login_successful, role, reauth = admin_login()

    if not login_successful and not reauth:
        logging.info("Unauthorized access. Returning to main menu.")
        return
    elif reauth:
        # The recursive call runs its own complete login-and-menu session. The
        # return is load-bearing: without it, when that inner session ends the
        # outer frame fell through into a SECOND menu on the unlocked role with
        # no password having been entered for it.
        logging.info("Reauthentication required. Please log in again.")
        admin_panel()
        return

    role = str(role).lower()
    # Shown on this panel's menu and every submenu opened from it: a shared console
    # must always display which operator account is live.
    session = f"Signed in as {_mod_session.CURRENT_USER} ({role})"

    if role == 'valet':
        logging.info("Enter Valet Panel...")
        ui.pause()
        valet_vehicle_management()
        return
    if role == 'it':
        logging.info("Enter IT Support Panel...")
        ui.pause()
        it_support_panel()
        return
    if role not in ('staff', 'manager', 'admin'):
        logging.error("A role has not been assigned. Please contact the system administrator.")
        ui.pause()
        return

    # The menu is data (_admin_menu): this loop renders it, and _run_admin_submenu
    # dispatches from the same tuples, so an entry cannot point at the wrong function.
    menu = _admin_menu(role)
    while True:
        ui.pause()
        ui.show_menu("Admin Panel",
                     [f"{i}. {label}" for i, (label, _entries) in enumerate(menu, 1)],
                     subtitle=session)
        choice = input("Enter your choice: ").strip()
        if not choice.isdigit() or not 1 <= int(choice) <= len(menu):
            logging.info("Invalid choice. Please try again.")
            continue
        label, entries = menu[int(choice) - 1]
        if entries is None:
            break  # "Exit Admin Panel"
        _run_admin_submenu(label, entries, session)
def view_discount_codes():
    """Display all discount codes as a table."""
    try:
        with db.get_connection() as conn:
            if conn is None:
                logging.info("Database connection failed.")
                return
            cursor = conn.cursor()
            cursor.execute("SELECT Code, DiscountPercentage FROM Discounts")
            discounts = cursor.fetchall()
            if discounts:
                table_rows = [(d.Code, f"{d.DiscountPercentage}%") for d in discounts]
                ui.show_table("Current Discount Codes", ["Code", "Percentage"], table_rows)
            else:
                ui.info("No discount codes found.")
    except Exception as e:
        logging.error(f"Error viewing discount codes: {e}")


def manage_discount_codes():
    """Provides a sub-menu for discount code management."""
    try:
        while True:
            ui.pause()
            ui.show_menu("Discount Code Management", [
                "1. Add Discount Code",
                "2. Update Discount Code",
                "3. Delete Discount Code",
                "4. View Discount Codes",
                "5. Exit Discount Management",
            ])
            choice = input("Enter your choice: ").strip()
            with db.get_connection() as conn:
                if conn is None:
                    logging.info("Database connection failed.")
                    return
                cursor = conn.cursor()
                if choice == "1":
                    cursor.execute("SELECT Code FROM Discounts")
                    last_code = cursor.fetchall()
                    logging.info(f"Last discount code: {(last_code[-1])[0] if last_code else 'None'}")
                    code = input("Enter new discount code: ").strip()
                    if not code:
                        logging.info("Discount code cannot be blank.")
                        continue
                    try:
                        discount_percentage = float(input("Enter discount percentage: ").strip())
                    except ValueError:
                        logging.info("Invalid discount percentage.")
                        continue
                    if discount_percentage <= 0 or discount_percentage > 100:
                        logging.info("Discount percentage must be greater than 0 and at most 100.")
                        continue
                    # Duplicate codes silently overwrote the first one (the PK is Code).
                    cursor.execute("SELECT 1 FROM Discounts WHERE Code = ?", (code,))
                    if cursor.fetchone():
                        logging.info(f"Discount code '{code}' already exists. Update it instead.")
                        continue
                    cursor.execute(
                        "INSERT INTO Discounts (Code, DiscountPercentage) VALUES (?, ?)",
                        (code, discount_percentage)
                    )
                    conn.commit()
                    core.log_audit("CREATE", "Discount", code, f"{discount_percentage}%")
                    logging.info(f"Discount code '{code}' added successfully.")
                elif choice == "2":
                    view_discount_codes()
                    code = input("Enter discount code to update: ").strip()
                    cursor.execute("SELECT DiscountPercentage FROM Discounts WHERE Code = ?", (code,))
                    existing = cursor.fetchone()
                    if existing:
                        try:
                            new_percentage = float(input("Enter new discount percentage: ").strip())
                        except ValueError:
                            logging.info("Invalid percentage.")
                            continue
                        if new_percentage <= 0 or new_percentage > 100:
                            logging.info("Discount percentage must be greater than 0 and at most 100.")
                            continue
                        cursor.execute(
                            "UPDATE Discounts SET DiscountPercentage = ? WHERE Code = ?",
                            (new_percentage, code)
                        )
                        conn.commit()
                        core.log_audit("UPDATE", "Discount", code,
                                  f"{float(existing[0])}% -> {new_percentage}%",
                                  old_value=f"{float(existing[0])}%",
                                  new_value=f"{new_percentage}%")
                        logging.info(f"Discount code '{code}' updated successfully.")
                    else:
                        logging.info("Discount code not found.")
                elif choice == "3":
                    view_discount_codes()
                    code = input("Enter discount code to delete: ").strip()
                    cursor.execute("SELECT DiscountPercentage FROM Discounts WHERE Code = ?", (code,))
                    existing = cursor.fetchone()
                    if not existing:
                        logging.info("Discount code not found.")
                        continue
                    if not ui.ask_confirmation(f"Delete discount code '{code}' ({float(existing[0])}%)?"):
                        continue
                    cursor.execute("DELETE FROM Discounts WHERE Code = ?", (code,))
                    conn.commit()
                    core.log_audit("DELETE", "Discount", code, f"Deleted {float(existing[0])}% code")
                    logging.info(f"Discount code '{code}' deleted successfully.")
                elif choice == "4":
                    view_discount_codes()
                elif choice == "5":
                    break
                else:
                    logging.info("Invalid choice. Please try again.")
    except Exception as e:
        logging.error(f"Error managing discount codes: {e}")
def reset_user_password():
    """Allow an admin to reset a user's password."""
    with db.get_connection() as conn:
        if conn is None:
            logging.info("Database connection failed.")
            return
        try:
            cursor = conn.cursor()
            username = input("Enter the username to reset password: ").strip()
            cursor.execute("SELECT Username FROM Users WHERE Username = ?", (username,))
            if cursor.fetchone():
                new_password = input("Enter new password: ").strip()
                cursor.execute("UPDATE Users SET Password = ? WHERE Username = ?", (new_password, username))
                conn.commit()
                core.log_audit("UPDATE", "User", username, "Password reset by admin")
                logging.info(f"Password for user '{username}' has been reset successfully.")
            else:
                logging.info("User not found.")
        except Exception as e:
            logging.error(f"Error resetting user password: {e}")

IT_SYSTEMS = ["Server", "Workstation", "POS Terminal", "WiFi Router", "Printer", "Database Server"]
IT_SOFTWARE = ["Microsoft Office", "Antivirus", "Hotel Management Suite", "Printer Driver",
               "Database Client", "Remote Desktop", "Web Browser"]
# Advice shown when simulated diagnostics fail on a given system.
IT_TROUBLESHOOTING = {
    "WiFi Router": "Check the WiFi signal strength and connectivity.",
    "Printer": "Check the printer connection and ink levels.",
    "Database Server": "Check the database connection and query performance.",
    "POS Terminal": "Check the POS terminal connection and transaction logs.",
    "Workstation": "Check the workstation connection and software updates.",
    "Server": "Check the server connection and resource utilization.",
}


def _pick_from_list(title, options):
    """Show a numbered list and return the chosen option, or None if cancelled."""
    ui.show_table(title, ["#", "Option"], [(i + 1, name) for i, name in enumerate(options)])
    while True:
        raw = input("Select an option (number, or blank to cancel): ").strip()
        if raw == "":
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1]
        logging.info(f"Invalid selection. Enter a number from 1 to {len(options)}.")


def it_network_configuration():
    """IT: record a network profile and optionally run simulated diagnostics."""
    network_name = input("Enter network name: ").strip()
    ip_address = input("Enter IP address: ").strip()
    subnet_mask = input("Enter subnet mask: ").strip()
    gateway = input("Enter gateway: ").strip()
    ui.success(f"Network '{network_name}' configured with IP {ip_address}, "
               f"Subnet {subnet_mask}, Gateway {gateway}.")
    if not ui.ask_confirmation("Run diagnostics and test the network?", default="y"):
        ui.info("Skipping diagnostics and test.")
        return
    logging.info("Beginning network diagnostics...")
    time.sleep(2)
    details = (f"Network Name: {network_name}\nIP Address: {ip_address}\n"
               f"Subnet Mask: {subnet_mask}\nGateway: {gateway}")
    if random.choice([True, False]):
        ui.box("Network Diagnostics - Result", details + "\n\nNo issues found.")
    else:
        ui.box("Network Diagnostics - Result", details + "\n\nIssues detected. "
               "Please check the network configuration.")


def it_user_accounts():
    """IT: the user-management actions, reusing the shared admin prompts."""
    ui.show_menu("User Account Management", [
        "1. Add User",
        "2. Remove User",
        "3. Reset Password",
        "4. Update User",
        "5. Back",
    ])
    actions = {'1': add_user, '2': delete_user, '3': reset_user_password, '4': edit_user}
    action = actions.get(input("Enter your choice: ").strip())
    if action:
        action()
    else:
        logging.info("Invalid action. Please choose 1-4, or 5 to go back.")


def it_system_diagnostics():
    """IT: simulated diagnostics on one of the managed systems."""
    system = _pick_from_list("Systems Available for Diagnostics", IT_SYSTEMS)
    if not system:
        return
    logging.info(f"Running diagnostics on {system}...")
    time.sleep(2)
    if random.choice([True, False]):
        ui.success(f"Diagnostics for {system} completed successfully. No issues found.")
    else:
        advice = IT_TROUBLESHOOTING.get(system, "Check the system configuration.")
        ui.box(f"Diagnostics - {system}",
               "Issues detected.\n\n" + advice + "\n\n"
               "(Note: diagnostics are simulated for demonstration.)")


def it_software_installation():
    """IT: simulated software install with a progress bar."""
    system = _pick_from_list("Systems Available for Installation", IT_SYSTEMS)
    if not system:
        return
    software = _pick_from_list("Applications Available for Installation", IT_SOFTWARE)
    if not software:
        return

    def format_size(bytes_size):
        units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
        size = float(bytes_size)
        for unit in units:
            if size < 1024:
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} PiB"

    # 200 KiB simulated transfer, chunked so the progress bar actually animates.
    total_size = 200 * 1024
    block_size = 200
    num_blocks = total_size // block_size

    logging.info(f"Installing {software} on {system}...")
    logging.info(f"Total size: {format_size(total_size)}")
    with tqdm.tqdm(total=num_blocks, desc="Simulated Download", unit="block", unit_scale=True) as bar:
        for _ in range(num_blocks):
            time.sleep(0.001)  # Simulate I/O delay
            bar.update(1)
    ui.success(f"{software} installed successfully on {system}.")


def it_support_panel():
    """IT Support: network config, user accounts, diagnostics, and software installs."""
    while True:
        ui.pause()
        ui.show_menu("IT Support Panel", [
            "1. Network Configuration",
            "2. User Account Management",
            "3. System Diagnostics",
            "4. Software Installation",
            "5. Exit IT Support Panel",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '1':
            it_network_configuration()
        elif choice == '2':
            it_user_accounts()
        elif choice == '3':
            it_system_diagnostics()
        elif choice == '4':
            it_software_installation()
        elif choice == '5':
            return
        else:
            logging.info("Invalid choice. Please try again.")
        ui.pause()

def valet_vehicle_management():
    """Valet: manage vehicle check-in/check-out and see what's parked."""
    while True:
        ui.pause()
        ui.show_menu("Valet: Vehicle Management", [
            "1. Check In a Vehicle",
            "2. Check Out a Vehicle",
            "3. View Parked Vehicles",
            "4. Exit",
        ])
        choice = input("Enter your choice: ").strip()
        if choice == '4':
            logging.info("Exiting Valet Vehicle Management.")
            return
        if choice == '3':
            _valet_list_parked()
            continue
        if choice not in ('1', '2'):
            logging.info("Invalid choice.")
            continue
        action = 'ci' if choice == '1' else 'co'
        license_plate = input("Enter vehicle license plate: ").strip()
        owner_name = input("Enter owner's name: ").strip()
        if not license_plate or not owner_name:
            logging.info("Both the license plate and the owner's name are required.")
            continue
        try:
            with db.get_connection() as conn:
                if conn is None:
                    logging.info("Database connection failed.")
                    return
                cursor = conn.cursor()
                if action == "ci":
                    parking_spot = input("Enter assigned parking spot: ").strip()
                    check_in_time = datetime.now()
                    cursor.execute(
                        "INSERT INTO ValetVehicles (LicensePlate, OwnerName, ParkingSpot, Status, CheckInTime) VALUES (?, ?, ?, ?, ?)",
                        (license_plate, owner_name, parking_spot, "Checked-In", check_in_time)
                    )
                    conn.commit()
                    logging.info(f"Vehicle {license_plate} checked in for {owner_name} at spot {parking_spot}.")
                else:
                    check_out_time = datetime.now()
                    cursor.execute(
                        "UPDATE ValetVehicles SET Status = ?, CheckOutTime = ? WHERE LicensePlate = ? AND OwnerName = ? AND Status = 'Checked-In'",
                        ("Checked-Out", check_out_time, license_plate, owner_name)
                    )
                    conn.commit()
                    if cursor.rowcount:
                        logging.info(f"Vehicle {license_plate} checked out for {owner_name}.")
                    else:
                        logging.info(f"No checked-in vehicle matches {license_plate} / {owner_name}.")
        except Exception as e:
            logging.error(f"Error managing valet vehicle: {e}")


def _valet_list_parked():
    """Valet: every vehicle currently flagged as checked in."""
    try:
        with db.get_connection() as conn:
            if conn is None:
                logging.info("Database connection failed.")
                return
            cursor = conn.cursor()
            cursor.execute(
                "SELECT LicensePlate, OwnerName, ParkingSpot, CheckInTime "
                "FROM ValetVehicles WHERE Status = 'Checked-In' ORDER BY CheckInTime"
            )
            rows = cursor.fetchall()
            if not rows:
                logging.info("No vehicles are currently checked in.")
                return
            ui.show_table(
                "Parked Vehicles",
                ["License Plate", "Owner", "Spot", "Checked In"],
                [(r[0], r[1], r[2], r[3]) for r in rows],
            )
    except Exception as e:
        logging.error(f"Error listing parked vehicles: {e}")
