# type: ignore
"""Button-driven Tkinter launcher for the hotel app."""
import main
import logging
from hotel import core
from hotel import tk_shell
from hotel import tk_admin
from hotel import tk_customer
from hotel import tk_bookings
from hotel import tk_conference
from hotel import onboarding


def main():
    core._ensure_database_config()
    try:
        from hotel import loyalty
        loyalty.ensure_loyalty_tables()
    except Exception:
        pass
    try:
        from hotel import rooms
        rooms.ensure_room_types_seeded()
    except Exception:
        pass
    if not onboarding.onboarding_completed():
        try:
            onboarding.run_first_run_onboarding()
        except (KeyboardInterrupt, EOFError):
            logging.info("Setup interrupted. Run the app again to continue setup.")
            return

    shell = tk_shell.Shell()
    import hotel.ui as uimod
    for name in ["clear_screen","pause","info","success","error","warning","box",
                 "show_menu","show_table","show_status_table","ask_number",
                 "ask_confirmation","ask_date","ask_optional_date","ask_text",
                 "ask_optional_text","open_clearance_window"]:
        setattr(uimod, name, getattr(shell, name))

    shell.add_sidebar_button("Customer", lambda: tk_customer.run_customer(shell))
    shell.add_sidebar_button("Admin", lambda: tk_admin.run_admin(shell))
    shell.add_sidebar_button("Bookings", lambda: tk_bookings.run_bookings(shell))
    shell.add_sidebar_button("Conference", lambda: tk_conference.run_conference(shell))
    shell.add_sidebar_button("Exit", lambda: shell.root.destroy())

    shell.show_buttons("Welcome", [
        ("Customer", lambda: tk_customer.run_customer(shell)),
        ("Admin", lambda: tk_admin.run_admin(shell)),
        ("Bookings", lambda: tk_bookings.run_bookings(shell)),
        ("Conference", lambda: tk_conference.run_conference(shell)),
    ])
    shell.run()


if __name__ == "__main__":
    main()
