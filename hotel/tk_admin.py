# type: ignore
"""Button-driven admin panel for the Tkinter shell."""
import logging
from hotel import admin
from hotel import session as _session


def run_admin(shell):
    login_successful, role, reauth = admin.admin_login()
    if not login_successful and not reauth:
        logging.info("Unauthorized access. Returning to main menu.")
        shell.pause()
        return
    if reauth:
        logging.info("Reauthentication required. Please log in again.")
        shell.pause()
        run_admin(shell)
        return
    role = str(role).lower()
    session_label = f"Signed in as {_session.CURRENT_USER} ({role})"
    if role == 'valet':
        shell.log_append("Enter Valet Panel...")
        shell.pause()
        admin.valet_vehicle_management()
        return
    if role == 'it':
        shell.log_append("Enter IT Support Panel...")
        shell.pause()
        admin.it_support_panel()
        return
    if role not in ('staff', 'manager', 'admin'):
        shell.error("A role has not been assigned. Please contact the system administrator.")
        shell.pause()
        return

    def show_category(cat_label, entries):
        items = []
        for (label, func, kwargs) in entries:
            items.append((label, lambda f=func, kw=kwargs: f(**kw) if kw else f()))
        items.append(("Back", lambda: show_top()))
        shell.show_buttons(cat_label, items)

    def show_top():
        menu = admin._admin_menu(role)
        items = []
        for (cat_label, entries) in menu:
            if entries is None:
                items.append((cat_label, lambda: None))
            else:
                items.append((cat_label, lambda cl=cat_label, en=entries: show_category(cl, en)))
        shell.show_buttons("Admin Panel - " + session_label, items)

    show_top()
    # block until user clicks Exit (which calls None) -> just wait
    # We'll let the shell's mainloop continue; but run_admin is called synchronously.
    # Use a simple wait: poll a flag via button? Instead, call shell's wait by not returning.
    # Simpler: show_top and return; navigation happens via buttons.
    return
